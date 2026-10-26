# -*- coding: utf-8 -*-
"""内存仓储：领域测试与无数据库场景使用。"""
from __future__ import annotations

import threading
from typing import Optional

from app.application.evidence.task_state import InMemoryTaskStateStore
from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.product import Product
from app.domain.order.order import Order
from app.domain.order.order_id import format_order_id, parse_order_sequence
from app.domain.order.ports.order_repository import OrderRepository
from app.infrastructure.persistence.seed_products import build_seed_products

__all__ = [
    "InMemoryOrderRepository",
    "InMemoryProductRepository",
    "InMemoryTaskStateStore",
]


class InMemoryProductRepository(ProductRepository):
    """内存商品仓储（测试 / 无数据库场景）。"""

    def __init__(self, products: Optional[list[Product]] = None) -> None:
        """可用显式商品列表覆盖默认种子数据。

        Args:
            products: 可选商品列表。
        """
        seed = products if products is not None else build_seed_products()
        self._products: dict[str, Product] = {p.product_id: p for p in seed}
        self._transaction_lock = threading.Lock()

    async def find_by_id(self, product_id: str) -> Optional[Product]:
        """按 ID 查询。

        Args:
            product_id: 商品 ID。

        Returns:
            Product；未命中返回 None。
        """
        return self._products.get(product_id)

    async def find_by_ids(self, product_ids: list[str]) -> list[Product]:
        """批量查询。

        Args:
            product_ids: 商品 ID 列表。

        Returns:
            命中的 Product 列表。
        """
        return [self._products[pid] for pid in product_ids if pid in self._products]

    async def list_all(self) -> list[Product]:
        """返回全部商品。

        Returns:
            Product 列表。
        """
        return list(self._products.values())

    async def count(self) -> int:
        """返回商品数。

        Returns:
            商品数。
        """
        return len(self._products)

    async def save(self, product: Product) -> None:
        """写入 / 更新商品。

        Args:
            product: 商品聚合。
        """
        self._products[product.product_id] = product

    async def insert_many(self, products: list[Product]) -> None:
        """批量写入商品。

        Args:
            products: 商品列表。
        """
        for product in products:
            self._products[product.product_id] = product


class InMemoryOrderRepository(OrderRepository):
    """内存订单仓储。"""

    def __init__(self) -> None:
        """初始化订单表、幂等索引与线程安全自增序列。"""
        self._orders: dict[str, Order] = {}
        self._idempotency_index: dict[str, str] = {}
        # 线程安全计数器：仓储实例可能被多线程 / 多任务共享（测试替身常见），
        # 取号与“已用最大号”推进都必须在同一把锁里完成。
        self._counter_lock = threading.Lock()
        self._next_sequence = 1

    def transaction_factory(self, product_repo):
        from app.infrastructure.persistence.in_memory_transactions import (
            InMemoryOrderTransactionFactory,
        )
        if not isinstance(product_repo, InMemoryProductRepository):
            raise ValueError("In-memory orders require in-memory product transactions")
        return InMemoryOrderTransactionFactory(product_repo, self)

    def _advance_sequence_past(self, order_id: str) -> None:
        """把内存序列推进到 ``order_id`` 之后，避免手工注入的订单号被重复发出。

        Args:
            order_id: 刚写入的订单号。
        """
        sequence = parse_order_sequence(order_id)
        if sequence is None:
            return
        with self._counter_lock:
            self._next_sequence = max(self._next_sequence, sequence + 1)

    async def save(self, order: Order) -> None:
        """保存订单。

        Args:
            order: 订单聚合。
        """
        previous = self._orders.get(order.order_id)
        if previous is not None and previous.idempotency_key:
            self._idempotency_index.pop(previous.idempotency_key, None)
        self._orders[order.order_id] = order
        if order.idempotency_key:
            self._idempotency_index[order.idempotency_key] = order.order_id
        # 兼容旧测试替身：直接塞进来的 GBX-0000xx 订单不能被后续取号覆盖。
        self._advance_sequence_past(order.order_id)

    async def find_by_id(self, order_id: str) -> Optional[Order]:
        """按订单号查询。

        Args:
            order_id: 订单号。

        Returns:
            Order；未命中返回 None。
        """
        return self._orders.get(order_id)

    async def find_by_idempotency_key(self, idempotency_key: str) -> Optional[Order]:
        """按幂等键查询订单。

        Args:
            idempotency_key: 下单幂等键。

        Returns:
            Order；未命中返回 None。
        """
        order_id = self._idempotency_index.get(idempotency_key)
        return self._orders.get(order_id) if order_id else None

    async def next_order_id(self) -> str:
        """线程安全地生成 ``GBX-000001`` 形式的订单号。

        Returns:
            订单号。
        """
        with self._counter_lock:
            sequence = self._next_sequence
            self._next_sequence += 1
        return format_order_id(sequence)

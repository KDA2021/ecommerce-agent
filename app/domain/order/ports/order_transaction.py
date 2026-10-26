"""Atomic order persistence boundary, independent of the database adapter."""

from __future__ import annotations

from typing import Protocol

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.order.ports.order_repository import OrderRepository


class RetryableOrderTransactionError(RuntimeError):
    """A deadlock or unique-key race rolled back the entire transaction."""


class OrderTransaction(Protocol):
    products: ProductRepository
    orders: OrderRepository

    async def __aenter__(self) -> "OrderTransaction": ...
    async def __aexit__(self, exc_type, exc, traceback) -> bool: ...


class OrderTransactionFactory(Protocol):
    def __call__(self) -> OrderTransaction: ...

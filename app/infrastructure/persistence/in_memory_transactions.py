"""Deterministic test adapter: serialize writes and undo all partial mutations."""

from __future__ import annotations

import asyncio
from copy import deepcopy


class InMemoryOrderTransactionFactory:
    def __init__(self, products, orders):
        self.products = products
        self.orders = orders

    def __call__(self):
        return InMemoryOrderTransaction(self.products, self.orders)


class InMemoryOrderTransaction:
    def __init__(self, products, orders):
        self.products = products
        self.orders = orders

    async def __aenter__(self):
        # A non-blocking threading lock also works when test callers use multiple loops.
        while not self.products._transaction_lock.acquire(blocking=False):
            await asyncio.sleep(0.001)
        try:
            self._products = dict(self.products._products)
            self._stocks = [(sku, sku.stock) for p in self._products.values() for sku in p.skus]
            self._orders = dict(self.orders._orders)
            self._order_states = [
                (order, deepcopy(order.__dict__)) for order in self._orders.values()
            ]
            self._index = dict(self.orders._idempotency_index)
        except BaseException:
            self.products._transaction_lock.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None:
                for sku, stock in self._stocks:
                    sku.stock = stock
                self.products._products = self._products
                for order, state in self._order_states:
                    order.__dict__.clear()
                    order.__dict__.update(state)
                self.orders._orders = self._orders
                self.orders._idempotency_index = self._index
        finally:
            self.products._transaction_lock.release()
        return False

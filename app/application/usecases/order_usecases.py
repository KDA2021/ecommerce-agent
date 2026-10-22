# -*- coding: utf-8 -*-
"""订单三个 UseCase：PlaceOrder / QueryOrder / CancelOrder

TradeAgent 的工具层只做参数搬运，业务规则（库存扣减、状态机、金额计算）全部收敛在这里与 Order 聚合内。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.product import Product
from app.domain.order.address import Address
from app.domain.order.order import Order
from app.domain.order.order_line import OrderLine
from app.domain.order.ports.order_repository import OrderRepository


@dataclass(frozen=True)
class OrderItemInput:
    """下单入参：商品 + SKU + 数量。"""

    product_id: str
    sku_id: str
    quantity: int


class PlaceOrderUseCase:
    """下单用例：校验商品 / SKU、扣库存、建单，失败时回滚库存。"""

    def __init__(self, product_repo: ProductRepository, order_repo: OrderRepository) -> None:
        """注入商品与订单仓储。

        Args:
            product_repo: 商品仓储。
            order_repo: 订单仓储。
        """
        self._product_repo = product_repo
        self._order_repo = order_repo

    async def execute(
        self,
        buyer_id: str,
        items: list[OrderItemInput],
        shipping_address: Address,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """执行下单并返回订单快照；提供幂等键时优先复用已存在订单。

        Args:
            buyer_id: 买家 id。
            items: 订单行入参。
            shipping_address: 收货地址。
            idempotency_key: 可选幂等键，同一键重复调用不再扣减库存 / 建新单。

        Returns:
            订单快照 dict；重放时额外带 ``idempotent_replay=True``。
        """
        if not items:
            raise ValueError("PlaceOrder.items 不能为空")

        if idempotency_key:
            existing = await self._order_repo.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                snapshot = existing.snapshot()
                snapshot["idempotent_replay"] = True
                return snapshot

        lines: list[OrderLine] = []
        deducted: list[tuple] = []  # 已扣减的 (sku, quantity)，失败时回滚
        touched: list[Product] = []  # 需要写回仓储的商品，失败时回滚库存
        try:
            for item in items:
                product = await self._product_repo.find_by_id(item.product_id)
                if product is None:
                    raise ValueError(f"商品不存在：{item.product_id}")
                sku = product.find_sku(item.sku_id)
                if sku is None:
                    raise ValueError(f"Sku 不存在：{item.product_id}/{item.sku_id}")
                sku.deduct_stock(item.quantity)
                if product not in touched:
                    touched.append(product)
                deducted.append((sku, item.quantity))
                lines.append(
                    OrderLine(
                        product_id=product.product_id,
                        sku_id=sku.sku_id,
                        title=f"{product.title}（{sku.spec}）",
                        unit_price=sku.price,
                        quantity=item.quantity,
                    ),
                )
            order = Order.place(
                order_id=await self._order_repo.next_order_id(),
                buyer_id=buyer_id,
                shipping_address=shipping_address,
                lines=lines,
                idempotency_key=idempotency_key,
            )
        except Exception:
            for sku, quantity in deducted:
                sku.restore_stock(quantity)
            for product in touched:
                await self._product_repo.save(product)
            raise
        for product in touched:
            await self._product_repo.save(product)
        await self._order_repo.save(order)
        snapshot = order.snapshot()
        if idempotency_key:
            snapshot["idempotent_replay"] = False
        return snapshot


class QueryOrderUseCase:
    """查单用例。"""

    def __init__(self, order_repo: OrderRepository) -> None:
        """注入订单仓储。

        Args:
            order_repo: 订单仓储。
        """
        self._order_repo = order_repo

    async def execute(self, order_id: str) -> dict:
        """按订单号查询订单快照。

        Args:
            order_id: 订单号。

        Returns:
            订单快照 dict。
        """
        order = await self._order_repo.find_by_id(order_id)
        if order is None:
            raise ValueError(f"订单不存在：{order_id}")
        return order.snapshot()


class CancelOrderUseCase:
    """取消用例：校验状态机并回补库存。"""

    def __init__(self, product_repo: ProductRepository, order_repo: OrderRepository) -> None:
        """注入商品与订单仓储。

        Args:
            product_repo: 商品仓储。
            order_repo: 订单仓储。
        """
        self._product_repo = product_repo
        self._order_repo = order_repo

    async def execute(self, order_id: str, reason: str) -> dict:
        """取消订单并返回最新快照。

        Args:
            order_id: 订单号。
            reason: 取消原因（必填）。

        Returns:
            订单快照 dict。
        """
        order = await self._order_repo.find_by_id(order_id)
        if order is None:
            raise ValueError(f"订单不存在：{order_id}")
        order.cancel(reason)
        # 取消后回补库存
        touched: list[Product] = []
        for line in order.lines:
            product = await self._product_repo.find_by_id(line.product_id)
            if product is not None:
                sku = product.find_sku(line.sku_id)
                if sku is not None:
                    sku.restore_stock(line.quantity)
                    if product not in touched:
                        touched.append(product)
        for product in touched:
            await self._product_repo.save(product)
        await self._order_repo.save(order)
        return order.snapshot()

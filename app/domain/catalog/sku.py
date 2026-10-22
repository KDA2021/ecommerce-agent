# -*- coding: utf-8 -*-
"""Sku 实体

Product（SPU）下的最小可售卖单元，携带价格与库存。
TradeAgent 创建订单时以 Sku 粒度结算。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.catalog.money import Money


@dataclass
class Sku:
    """可售卖单元：规格 + 价格 + 库存。"""

    sku_id: str
    spec: str  # 规格描述，如 "黑色 / 20寸"
    price: Money
    stock: int

    def __post_init__(self) -> None:
        """校验 sku_id 非空与库存非负。"""
        if not self.sku_id:
            raise ValueError("Sku.sku_id required")
        if self.stock < 0:
            raise ValueError(f"Sku.stock 必须非负：{self.sku_id}")

    def has_stock(self, quantity: int) -> bool:
        """判断库存是否够 quantity 件。

        Args:
            quantity: 需要的件数。

        Returns:
            是否可满足。
        """
        return self.stock >= quantity

    def deduct_stock(self, quantity: int) -> None:
        """扣减库存，不足时抛 ValueError。

        Args:
            quantity: 扣减件数。
        """
        if not self.has_stock(quantity):
            raise ValueError(f"Sku 库存不足：{self.sku_id}，剩余 {self.stock}，需要 {quantity}")
        self.stock -= quantity

    def restore_stock(self, quantity: int) -> None:
        """回补库存（取消订单 / 下单回滚时使用）。

        Args:
            quantity: 回补件数。
        """
        if quantity < 0:
            raise ValueError("restore_stock.quantity 必须非负")
        self.stock += quantity

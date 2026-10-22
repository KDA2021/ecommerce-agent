# -*- coding: utf-8 -*-
"""Product 聚合根

Globex 把跨境商品建模为 Product（SPU）+ Sku（多个），携带品牌、产地、亮点等结构化属性。
SearchAgent 召回的"候选集"传递的就是 Product 卡片，TradeAgent 创建订单时再以 Sku 粒度结算。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app.domain.catalog.sku import Sku


@dataclass(frozen=True)
class ProductHighlight:
    """商品亮点：标签 + 说明。"""

    label: str        # 亮点短标签，如“轻便 / 防水”
    detail: str = ""  # 亮点补充说明，可为空


@dataclass
class Product:
    """商品聚合根（SPU），内含多个 SKU。"""

    product_id: str                              # SPU 唯一标识，如 P1001
    title: str                                   # 商品标题
    brand: str                                   # 品牌名
    category: str                                # 品类，如 travel
    origin_country: str                          # 产地国家二位码
    description: str                             # 商品描述
    highlights: list[ProductHighlight] = field(default_factory=list)  # 结构化卖点
    ships_to: list[str] = field(default_factory=list)                 # 可发货国家二位码列表
    skus: list[Sku] = field(default_factory=list)                      # 该 SPU 下的 SKU 列表

    def __post_init__(self) -> None:
        """校验 product_id 非空且至少有一个 SKU。"""
        if not self.product_id:
            raise ValueError("Product.product_id required")
        if not self.skus:
            raise ValueError(f"Product 至少要有一个 Sku：{self.product_id}")

    def primary_sku(self) -> Sku:
        """返回首个 SKU，作为主价格口径。

        Returns:
            主 SKU。
        """
        return self.skus[0]

    def find_sku(self, sku_id: str) -> Optional[Sku]:
        """按 sku_id 查找。

        Args:
            sku_id: SKU 标识。

        Returns:
            Sku；未命中返回 None。
        """
        return next((s for s in self.skus if s.sku_id == sku_id), None)

    def searchable_text(self) -> str:
        """召回用的可检索文本：标题 + 品牌 + 品类 + 描述 + 亮点。

        Returns:
            用于召回与关键词检索的拼接文本。
        """
        highlight_text = " ".join(f"{h.label} {h.detail}" for h in self.highlights)
        return " ".join(
            [self.title, self.brand, self.category, self.origin_country, self.description, highlight_text],
        )

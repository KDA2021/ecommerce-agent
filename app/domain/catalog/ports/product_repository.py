# -*- coding: utf-8 -*-
"""ProductRepository 端口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.domain.catalog.product import Product


class ProductRepository(ABC):
    """商品仓储端口，domain 不关心具体实现。"""

    @abstractmethod
    async def find_by_id(self, product_id: str) -> Optional[Product]:
        """按商品 ID 查询。

        Args:
            product_id: 商品 ID。

        Returns:
            Product；未命中返回 None。
        """
        ...

    @abstractmethod
    async def find_by_ids(self, product_ids: list[str]) -> list[Product]:
        """批量按 ID 查询。

        Args:
            product_ids: 商品 ID 列表。

        Returns:
            命中的 Product 列表。
        """
        ...

    @abstractmethod
    async def list_all(self) -> list[Product]:
        """返回全部商品。

        Returns:
            Product 列表。
        """
        ...

    @abstractmethod
    async def count(self) -> int:
        """返回商品总数。

        Returns:
            商品数。
        """
        ...

    @abstractmethod
    async def save(self, product: Product) -> None:
        """保存 / 更新单个商品。

        Args:
            product: 商品聚合。
        """
        ...

    @abstractmethod
    async def insert_many(self, products: list[Product]) -> None:
        """批量插入商品（已存在则跳过）。

        Args:
            products: 商品列表。
        """
        ...

# -*- coding: utf-8 -*-
"""OrderRepository 端口

Domain 不关心实现，Infrastructure 当前提供 MySQL 实现，内存实现仅用于测试。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.domain.order.order import Order


class OrderRepository(ABC):
    """订单仓储端口。"""

    @abstractmethod
    async def save(self, order: Order) -> None:
        """保存 / 更新订单。

        Args:
            order: 订单聚合。
        """
        ...

    @abstractmethod
    async def find_by_id(self, order_id: str) -> Optional[Order]:
        """按订单号查询。

        Args:
            order_id: 订单号。

        Returns:
            Order；未命中返回 None。
        """
        ...

    @abstractmethod
    async def find_by_idempotency_key(self, idempotency_key: str) -> Optional[Order]:
        """按幂等键查询订单，用于请求重放去重。

        Args:
            idempotency_key: 下单幂等键。

        Returns:
            Order；未命中返回 None。
        """
        ...

    @abstractmethod
    async def next_order_id(self) -> str:
        """生成下一个订单号。

        实现必须保证并发唯一：MySQL 实现走 ``order_counters`` 行级原子自增
        （``UPDATE ... LAST_INSERT_ID``），InMemory 实现走线程安全计数器。
        调用方（PlaceOrderUseCase）只负责拿号，不负责查重。

        Returns:
            订单号字符串，形如 ``GBX-000042``。
        """
        ...

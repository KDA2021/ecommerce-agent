# -*- coding: utf-8 -*-
"""Address 值对象

跨境收货地址。country 用于关税/运费口径（MVP 仅存储与展示）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Address:
    """跨境收货地址值对象。"""

    recipient_name: str
    country: str
    state: str
    city: str
    address_line: str
    postal_code: str
    phone: str

    def __post_init__(self) -> None:
        """校验收件人 / 国家 / 城市 / 地址行等必填字段。"""
        for field_name in ("recipient_name", "country", "city", "address_line"):
            if not getattr(self, field_name):
                raise ValueError(f"Address.{field_name} required")

    def one_line(self) -> str:
        """渲染成单行地址串。

        Returns:
            单行地址。
        """
        return f"{self.country} {self.state} {self.city} {self.address_line}（{self.recipient_name} {self.phone}）"
